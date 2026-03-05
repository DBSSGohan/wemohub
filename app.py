"""
WeMo Controller Backend
Run: pip install flask pywemo apscheduler flask-cors
Then: python app.py
Access: http://localhost:5000
"""

from flask import Flask, jsonify, request, send_from_directory
from flask_cors import CORS
from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger
from apscheduler.triggers.date import DateTrigger
import pywemo
import json
import os
import threading
import time
import logging
from datetime import datetime, timedelta

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

app = Flask(__name__, static_folder='static')
CORS(app)

# ── In-memory state ──────────────────────────────────────────────────────────
discovered_devices = {}   # name -> device object
device_aliases    = {}    # name -> alias  (persisted to aliases.json)
timers            = {}    # timer_id -> timer info
smart_rules       = {}    # rule_id -> rule info
device_state_cache = {}   # name -> {state, last_updated}

ALIASES_FILE = "aliases.json"
TIMERS_FILE  = "timers.json"
RULES_FILE   = "rules.json"

scheduler = BackgroundScheduler(daemon=True)
scheduler.start()

# ── Persistence helpers ──────────────────────────────────────────────────────
def load_json(path):
    if os.path.exists(path):
        with open(path) as f:
            return json.load(f)
    return {}

def save_json(path, data):
    with open(path, "w") as f:
        json.dump(data, f, indent=2)

def load_persistent():
    global device_aliases, timers, smart_rules
    device_aliases = load_json(ALIASES_FILE)
    timers         = load_json(TIMERS_FILE)
    smart_rules    = load_json(RULES_FILE)

load_persistent()

# ── Device discovery ─────────────────────────────────────────────────────────
WEMO_PORTS = [49153, 49152, 49154, 49155, 49156, 49157]

def get_local_subnet():
    """Get the local machine's subnet (e.g. 192.168.1)"""
    import socket
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("8.8.8.8", 80))
        ip = s.getsockname()[0]
        s.close()
        return ".".join(ip.split(".")[:3])
    except Exception:
        return "192.168.1"

def probe_ip(ip, found_devices, lock):
    """Try to connect to a single IP on known WeMo ports"""
    import socket
    for port in WEMO_PORTS:
        try:
            # Quick TCP check first
            s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            s.settimeout(0.3)
            result = s.connect_ex((ip, port))
            s.close()
            if result == 0:
                # Port is open — try pywemo
                url = pywemo.setup_url_for_address(ip, port)
                device = pywemo.discovery.device_from_description(url)
                if device:
                    logger.info(f"Found WeMo device at {ip}:{port} — {device.name}")
                    with lock:
                        found_devices[device.name] = device
                    return  # found on this IP, no need to try other ports
        except Exception:
            pass

def discover_devices():
    global discovered_devices
    logger.info("Discovering WeMo devices — scanning subnet...")

    found_devices = {}
    lock = threading.Lock()

    # 1. Try standard UPnP multicast first (fast, works on some networks)
    try:
        devices = pywemo.discover_devices(timeout=4)
        for d in devices:
            found_devices[d.name] = d
            logger.info(f"Multicast found: {d.name}")
    except Exception as e:
        logger.warning(f"Multicast discovery failed: {e}")

    # 2. Subnet scan — probe every IP on the local network
    subnet = get_local_subnet()
    logger.info(f"Scanning subnet {subnet}.1-254 ...")
    threads = []
    for i in range(1, 255):
        ip = f"{subnet}.{i}"
        t = threading.Thread(target=probe_ip, args=(ip, found_devices, lock), daemon=True)
        threads.append(t)
        t.start()
        # Throttle slightly to avoid overwhelming router
        if i % 50 == 0:
            time.sleep(0.1)

    for t in threads:
        t.join(timeout=3)

    discovered_devices = found_devices
    for name in discovered_devices:
        if name not in device_state_cache:
            device_state_cache[name] = {"state": None, "last_updated": None}

    logger.info(f"Discovery complete. Found {len(discovered_devices)} device(s): {list(discovered_devices.keys())}")
    return list(discovered_devices.keys())

def get_device_state(device_name):
    device = discovered_devices.get(device_name)
    if not device:
        return None
    try:
        state = device.get_state()
        device_state_cache[device_name] = {"state": state, "last_updated": datetime.now().isoformat()}
        return state
    except Exception as e:
        logger.error(f"State error for {device_name}: {e}")
        cached = device_state_cache.get(device_name, {})
        return cached.get("state")

def set_device_state(device_name, state):
    device = discovered_devices.get(device_name)
    if not device:
        return False, "Device not found"
    try:
        if state:
            device.on()
        else:
            device.off()
        device_state_cache[device_name] = {"state": 1 if state else 0, "last_updated": datetime.now().isoformat()}
        # Evaluate smart rules after state change
        threading.Thread(target=evaluate_rules, args=(device_name, state), daemon=True).start()
        return True, "OK"
    except Exception as e:
        return False, str(e)

def device_info(name):
    device = discovered_devices.get(name)
    state  = get_device_state(name)
    alias  = device_aliases.get(name, name)
    d_type = type(device).__name__ if device else "Unknown"
    return {
        "name": name,
        "alias": alias,
        "type": d_type,
        "state": state,
        "last_updated": device_state_cache.get(name, {}).get("last_updated"),
    }

# ── Scheduling helpers ────────────────────────────────────────────────────────
def run_timer_action(timer_id):
    t = timers.get(timer_id)
    if not t:
        return
    logger.info(f"Running timer {timer_id}: {t}")
    ok, msg = set_device_state(t["device"], t["action"] == "on")
    if not ok:
        logger.error(f"Timer {timer_id} failed: {msg}")
    # If one-shot, remove
    if t.get("repeat") == "once":
        timers.pop(timer_id, None)
        save_json(TIMERS_FILE, timers)
        try:
            scheduler.remove_job(timer_id)
        except Exception:
            pass

def schedule_timer(timer_id, timer):
    try:
        scheduler.remove_job(timer_id)
    except Exception:
        pass

    repeat = timer.get("repeat", "once")
    if repeat == "once":
        run_at = datetime.fromisoformat(timer["run_at"])
        scheduler.add_job(run_timer_action, DateTrigger(run_date=run_at), id=timer_id, args=[timer_id])
    else:
        # cron-style: time like "HH:MM", days like "mon,tue" or "daily"
        t     = timer["time"].split(":")
        hour  = int(t[0])
        minute= int(t[1])
        days  = timer.get("days", "daily")
        if days == "daily":
            trigger = CronTrigger(hour=hour, minute=minute)
        elif days == "weekdays":
            trigger = CronTrigger(hour=hour, minute=minute, day_of_week="mon-fri")
        elif days == "weekends":
            trigger = CronTrigger(hour=hour, minute=minute, day_of_week="sat,sun")
        else:
            trigger = CronTrigger(hour=hour, minute=minute, day_of_week=days)
        scheduler.add_job(run_timer_action, trigger, id=timer_id, args=[timer_id])

def restore_timers():
    for tid, t in timers.items():
        try:
            schedule_timer(tid, t)
        except Exception as e:
            logger.error(f"Failed to restore timer {tid}: {e}")

# ── Smart rules ───────────────────────────────────────────────────────────────
def evaluate_rules(trigger_device, trigger_state):
    for rule_id, rule in smart_rules.items():
        if not rule.get("enabled", True):
            continue
        if rule["trigger_device"] == trigger_device:
            trigger_on = rule["trigger_on"]  # True = fire when ON, False = fire when OFF
            if bool(trigger_state) == trigger_on:
                action_device = rule["action_device"]
                action = rule["action"]
                logger.info(f"Rule {rule_id}: {trigger_device} → {action_device} {action}")
                set_device_state(action_device, action == "on")

# ── API Routes ────────────────────────────────────────────────────────────────

@app.route("/")
def index():
    return send_from_directory("static", "index.html")

@app.route("/api/discover", methods=["POST"])
def api_discover():
    names = discover_devices()
    devices = [device_info(n) for n in names]
    return jsonify({"devices": devices})

@app.route("/api/devices", methods=["GET"])
def api_devices():
    devices = [device_info(n) for n in discovered_devices]
    return jsonify({"devices": devices})

@app.route("/api/devices/<name>/state", methods=["GET"])
def api_get_state(name):
    state = get_device_state(name)
    return jsonify({"name": name, "state": state})

@app.route("/api/devices/<name>/toggle", methods=["POST"])
def api_toggle(name):
    current = get_device_state(name)
    ok, msg = set_device_state(name, not current)
    new_state = get_device_state(name)
    return jsonify({"ok": ok, "msg": msg, "state": new_state})

@app.route("/api/devices/<name>/on", methods=["POST"])
def api_on(name):
    ok, msg = set_device_state(name, True)
    return jsonify({"ok": ok, "msg": msg, "state": 1})

@app.route("/api/devices/<name>/off", methods=["POST"])
def api_off(name):
    ok, msg = set_device_state(name, False)
    return jsonify({"ok": ok, "msg": msg, "state": 0})

@app.route("/api/devices/<name>/alias", methods=["POST"])
def api_alias(name):
    data = request.json
    device_aliases[name] = data.get("alias", name)
    save_json(ALIASES_FILE, device_aliases)
    return jsonify({"ok": True})

# ── Timer routes ──────────────────────────────────────────────────────────────
@app.route("/api/timers", methods=["GET"])
def api_list_timers():
    return jsonify({"timers": timers})

@app.route("/api/timers", methods=["POST"])
def api_add_timer():
    data = request.json
    tid  = f"timer_{int(time.time()*1000)}"
    timers[tid] = {**data, "id": tid}
    save_json(TIMERS_FILE, timers)
    schedule_timer(tid, timers[tid])
    return jsonify({"ok": True, "id": tid})

@app.route("/api/timers/<tid>", methods=["DELETE"])
def api_delete_timer(tid):
    timers.pop(tid, None)
    save_json(TIMERS_FILE, timers)
    try:
        scheduler.remove_job(tid)
    except Exception:
        pass
    return jsonify({"ok": True})

# ── Smart Rule routes ─────────────────────────────────────────────────────────
@app.route("/api/rules", methods=["GET"])
def api_list_rules():
    return jsonify({"rules": smart_rules})

@app.route("/api/rules", methods=["POST"])
def api_add_rule():
    data = request.json
    rid  = f"rule_{int(time.time()*1000)}"
    smart_rules[rid] = {**data, "id": rid, "enabled": True}
    save_json(RULES_FILE, smart_rules)
    return jsonify({"ok": True, "id": rid})

@app.route("/api/rules/<rid>", methods=["DELETE"])
def api_delete_rule(rid):
    smart_rules.pop(rid, None)
    save_json(RULES_FILE, smart_rules)
    return jsonify({"ok": True})

@app.route("/api/rules/<rid>/toggle", methods=["POST"])
def api_toggle_rule(rid):
    if rid in smart_rules:
        smart_rules[rid]["enabled"] = not smart_rules[rid].get("enabled", True)
        save_json(RULES_FILE, smart_rules)
    return jsonify({"ok": True, "enabled": smart_rules.get(rid, {}).get("enabled")})

# ── Alexa Lambda endpoint (for custom Alexa skill) ────────────────────────────
@app.route("/alexa", methods=["POST"])
def alexa_endpoint():
    body = request.json
    request_type = body.get("request", {}).get("type", "")

    if request_type == "LaunchRequest":
        return jsonify({
            "version": "1.0",
            "response": {
                "outputSpeech": {"type": "PlainText", "text": "WeMo controller ready. Say turn on or turn off followed by a device name."},
                "shouldEndSession": False
            }
        })

    if request_type == "IntentRequest":
        intent = body["request"]["intent"]["name"]
        slots  = body["request"]["intent"].get("slots", {})
        device_slot = slots.get("DeviceName", {}).get("value", "")

        # Find device by alias or name
        matched = None
        for name, alias in {**{n: n for n in discovered_devices}, **device_aliases}.items():
            if alias.lower() == device_slot.lower() or name.lower() == device_slot.lower():
                matched = name
                break

        if intent == "TurnOnIntent" and matched:
            set_device_state(matched, True)
            speech = f"Turned on {device_slot}"
        elif intent == "TurnOffIntent" and matched:
            set_device_state(matched, False)
            speech = f"Turned off {device_slot}"
        elif intent == "ToggleIntent" and matched:
            current = get_device_state(matched)
            set_device_state(matched, not current)
            speech = f"Toggled {device_slot}"
        else:
            speech = f"I couldn't find a device named {device_slot}"

        return jsonify({
            "version": "1.0",
            "response": {
                "outputSpeech": {"type": "PlainText", "text": speech},
                "shouldEndSession": True
            }
        })

    return jsonify({"version": "1.0", "response": {"outputSpeech": {"type": "PlainText", "text": "OK"}, "shouldEndSession": True}})

@app.route("/api/shutdown", methods=["POST"])
def api_shutdown():
    import signal
    logger.info("Shutdown requested via UI")
    threading.Thread(target=lambda: (time.sleep(0.5), os.kill(os.getpid(), signal.SIGTERM)), daemon=True).start()
    return jsonify({"ok": True})

if __name__ == "__main__":
    restore_timers()
    # Initial discovery in background
    threading.Thread(target=discover_devices, daemon=True).start()
    app.run(host="0.0.0.0", port=8080, debug=False)
