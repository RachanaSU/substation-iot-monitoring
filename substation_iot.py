"""
Substation Monitoring using IoT - simulation
Author: Rachana Santhebachahalli Umesha

Architecture (simulated end to end, no hardware needed):
  Field sensors -> ESP32-style edge node -> MQTT broker -> cloud monitor -> alarms + dashboard

Simulates 24 hours of an 11 kV / 415 V distribution substation sampled every minute.
A heat-wave overload in the afternoon and a failing station battery are injected so the
monitor has real faults to catch.
"""
import json
from collections import defaultdict

import numpy as np
import matplotlib.pyplot as plt
import matplotlib.dates as mdates
from datetime import datetime, timedelta

rng = np.random.default_rng(7)

# ---------------- Substation model ----------------
RATED_KVA = 500            # transformer rating
RATED_I_LV = RATED_KVA * 1000 / (np.sqrt(3) * 415)   # ~696 A rated LV current
AMBIENT_C = 18.0
TOP_OIL_RISE_RATED = 55.0  # top-oil rise above ambient at full load (C)
OIL_TIME_CONST_MIN = 90    # thermal time constant of the oil (minutes)


def load_profile(minute):
    """Per-unit load over a day: morning and evening peaks, plus an afternoon overload."""
    h = minute / 60
    base = 0.45 + 0.25 * np.exp(-((h - 8.5) / 1.5) ** 2) + 0.35 * np.exp(-((h - 19) / 2) ** 2)
    if 13 <= h < 17:                 # heat wave: air-conditioning load pushes it past rating
        base += 0.70
    return base + rng.normal(0, 0.02)


class SubstationSensors:
    """Field sensors: CT for current, PT for voltage, PT100 for oil temperature, battery monitor."""

    def __init__(self):
        self.top_oil = AMBIENT_C + 20
        self.battery_v = 112.0

    def sample(self, minute):
        k = load_profile(minute)
        # First-order thermal model: oil temperature chases the load-dependent steady state
        target = AMBIENT_C + TOP_OIL_RISE_RATED * k ** 1.6
        self.top_oil += (target - self.top_oil) / OIL_TIME_CONST_MIN
        # Voltage drops as load rises (source impedance)
        v_lv = 415 * (1 - 0.04 * k) + rng.normal(0, 1.0)
        # Station battery: charger fails at 19:00, so the battery slowly drains
        if minute >= 19 * 60:
            self.battery_v -= 0.03
        return {
            "current_A": round(k * RATED_I_LV + rng.normal(0, 5), 1),
            "voltage_V": round(v_lv, 1),
            "oil_temp_C": round(self.top_oil + rng.normal(0, 0.3), 1),
            "battery_V": round(self.battery_v + rng.normal(0, 0.05), 2),
            "load_pu": round(k, 3),
        }


# ---------------- MQTT (minimal in-process broker) ----------------
class MiniBroker:
    """Stands in for an MQTT broker such as Mosquitto: topic-based publish/subscribe."""

    def __init__(self):
        self.subs = defaultdict(list)
        self.published = 0

    def subscribe(self, prefix, callback):
        self.subs[prefix].append(callback)

    def publish(self, topic, payload):
        self.published += 1
        for prefix, cbs in self.subs.items():
            if topic.startswith(prefix):
                for cb in cbs:
                    cb(topic, payload)


class EdgeNode:
    """ESP32-style node in the substation: reads sensors and publishes JSON over Wi-Fi/MQTT."""

    def __init__(self, site, broker):
        self.site, self.broker, self.sensors = site, broker, SubstationSensors()

    def tick(self, ts, minute):
        reading = self.sensors.sample(minute)
        payload = json.dumps({"ts": ts.isoformat(), **reading})
        self.broker.publish(f"substation/{self.site}/telemetry", payload)


# ---------------- Cloud monitor ----------------
LIMITS = {
    "current_A":  {"high": RATED_I_LV},          # overload above rated current
    "voltage_V":  {"low": 390, "high": 440},     # +/- ~6% of 415 V
    "oil_temp_C": {"high": 85},                  # top-oil alarm
    "battery_V":  {"low": 105},                  # 110 V DC system
}
# Hysteresis: an alarm clears only once the value is back inside the limit by this margin,
# and it is raised only after 2 consecutive bad samples. This stops alarm "chattering".
DEADBAND = {"current_A": 0.05 * RATED_I_LV, "voltage_V": 3, "oil_temp_C": 3, "battery_V": 1}
CONFIRM_SAMPLES = 2
OIL_RISE_RATE_LIMIT = 0.25  # C per minute over 10 min: early warning before the absolute limit


class CloudMonitor:
    def __init__(self, broker):
        self.history = []
        self.events = []
        self.active = set()
        self.pending = defaultdict(int)
        broker.subscribe("substation/", self.on_message)

    def on_message(self, topic, payload):
        r = json.loads(payload)
        r["ts"] = datetime.fromisoformat(r["ts"])
        self.history.append(r)
        bad = set()      # limits breached by this sample
        ok = set()       # alarms whose value is back inside limit minus the deadband
        for k, lim in LIMITS.items():
            if "high" in lim:
                if r[k] > lim["high"]:
                    bad.add(f"HIGH {k}")
                elif r[k] < lim["high"] - DEADBAND[k]:
                    ok.add(f"HIGH {k}")
            if "low" in lim:
                if r[k] < lim["low"]:
                    bad.add(f"LOW {k}")
                elif r[k] > lim["low"] + DEADBAND[k]:
                    ok.add(f"LOW {k}")
        if len(self.history) > 10:
            rate = (r["oil_temp_C"] - self.history[-11]["oil_temp_C"]) / 10
            if rate > OIL_RISE_RATE_LIMIT:
                bad.add("FAST oil_temp_C rise")
            elif rate < OIL_RISE_RATE_LIMIT / 2:      # clears only once the rise has clearly slowed
                ok.add("FAST oil_temp_C rise")
        for a in bad:
            self.pending[a] += 1
            if a not in self.active and self.pending[a] >= CONFIRM_SAMPLES:
                self.active.add(a)
                self.events.append((r["ts"], "RAISED", a, r))
        for a in list(self.pending):
            if a not in bad:
                self.pending[a] = 0
        for a in self.active & ok:
            self.active.discard(a)
            self.events.append((r["ts"], "CLEARED", a, r))


# ---------------- Run 24 hours ----------------
def main():
    broker = MiniBroker()
    monitor = CloudMonitor(broker)
    node = EdgeNode("dublin-north-11kv", broker)
    start = datetime(2026, 7, 15, 0, 0)
    for m in range(24 * 60):
        node.tick(start + timedelta(minutes=m), m)

    h = monitor.history
    print(f"Messages published over MQTT: {broker.published}")
    print(f"Sample payload: {json.dumps({k: (v.isoformat() if k == 'ts' else v) for k, v in h[0].items()})}")
    print(f"Peak load: {max(x['load_pu'] for x in h):.2f} pu | "
          f"peak oil temp: {max(x['oil_temp_C'] for x in h):.1f} C | "
          f"min LV voltage: {min(x['voltage_V'] for x in h):.1f} V | "
          f"min battery: {min(x['battery_V'] for x in h):.2f} V")
    print("\nAlarm log:")
    for ts, kind, name, r in monitor.events:
        print(f"  {ts:%H:%M}  {kind:<7} {name}")

    # Dashboard
    t = [x["ts"] for x in h]
    fig, ax = plt.subplots(4, 1, figsize=(10, 10), sharex=True)
    panels = [("current_A", "LV current (A)"), ("voltage_V", "LV voltage (V)"),
              ("oil_temp_C", "Transformer top-oil temp (C)"), ("battery_V", "Station battery (V DC)")]
    for a, (k, label) in zip(ax, panels):
        a.plot(t, [x[k] for x in h], lw=1)
        for edge, val in LIMITS[k].items():
            a.axhline(val, ls="--", c="tab:red", lw=1)
            a.text(t[5], val, f" {edge} limit {val:.0f}", va="bottom", fontsize=8, color="tab:red")
        a.set_ylabel(label, fontsize=9); a.grid(alpha=0.3)
    for ts, kind, name, _ in monitor.events:
        if kind == "RAISED":
            for a in ax:
                a.axvline(ts, c="tab:orange", alpha=0.25, lw=1)
    ax[0].set_title("Substation dashboard: 24 h of IoT telemetry (orange lines = alarms raised)")
    ax[-1].xaxis.set_major_formatter(mdates.DateFormatter("%H:%M"))
    ax[-1].set_xlabel("Time of day")
    fig.tight_layout(); fig.savefig("dashboard.png", dpi=150)
    print("\nSaved dashboard.png")


if __name__ == "__main__":
    main()
