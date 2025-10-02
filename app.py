#!/usr/bin/env python3
# -*- coding: utf-8 -*-

import json, os, platform, random, subprocess, sys, time, threading, stat
from pathlib import Path
from typing import Dict, List, Optional, Any
from abc import ABC, abstractmethod
from datetime import datetime

# ---------------- Flask + basics ----------------
try:
    from flask import Flask, jsonify, request
    from flask_cors import CORS
    import pyautogui
except Exception as e:
    print("Missing required dependency:", e)
    print("Install with: pip install Flask Flask-Cors pyautogui")
    sys.exit(1)

# Optional: load .env if present (only if python-dotenv installed)
try:
    from dotenv import load_dotenv  # pip install python-dotenv
    load_dotenv()
except Exception:
    pass

# Playwright (optional, used for mobile)
try:
    from playwright.sync_api import sync_playwright, TimeoutError as PWTimeout  # type: ignore
    _HAVE_PLAYWRIGHT = True
except Exception:
    _HAVE_PLAYWRIGHT = False

# ---------------- Configuration ----------------
PROFILE_INFO_FILE = "profile_info.json"
AI_CONFIG_FILE    = "ai_config.json"  # where the Web UI-saved API keys live (server-side file)

# Mobile search config (env-overridable)
MOBILE_SEARCH_COUNT = int(os.getenv("MOBILE_SEARCH_COUNT", "20"))
MOBILE_DEVICE   = os.getenv("MOBILE_DEVICE", "Pixel 7")
MOBILE_TIMEZONE = os.getenv("MOBILE_TIMEZONE", "Asia/Dhaka")
MOBILE_LOCALE   = os.getenv("MOBILE_LOCALE", "en-US")
MOBILE_HEADLESS = os.getenv("MOBILE_HEADLESS", "1") == "1"

# Legacy UA window fallback (when Playwright isn't available)
MOBILE_UA = os.getenv(
    "MOBILE_UA",
    "Mozilla/5.0 (Linux; Android 12; Pixel 5) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/118.0.0.0 Mobile Safari/537.36"
)
MOBILE_WINDOW_SIZE = os.getenv("MOBILE_WINDOW_SIZE", "390,844")

# ---------------- App + State ----------------
app = Flask(__name__, static_folder=".", static_url_path="")
CORS(app)

state_lock = threading.Lock()
state: Dict[str, Any] = {
    "is_running": False,
    "status": "Ready",
    "progress": 0.0,
    "current_search": "",
    "current_profile": "",
    "completed": 0,      # desktop-only
    "total": 0,          # desktop-only
    "is_paused": False,
    "profile_progress": {},     # { profile: {done,total} } for desktop
    "profile_points": {},       # UI compat
    "profile_eligibility": {},  # { profile: {mobile:bool,reason} }
    "mobile_enabled": False,
    "mobile_progress": {},      # { profile: {done,total} }
}
selected_profiles_memory = {"chrome": [], "edge": []}
worker_thread: Optional[threading.Thread] = None

# ---------------- Helpers: storage ----------------
def _today_str() -> str:
    return datetime.now().date().isoformat()

def _load_json(path: str) -> dict:
    p = Path(path)
    if not p.exists():
        return {}
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except:
        return {}

def _write_secure_json(path: str, data: dict):
    """
    Save JSON with restrictive permissions (600) on POSIX. On Windows we still write normally.
    """
    p = Path(path)
    tmp = p.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    try:
        if os.name == "posix":
            os.chmod(tmp, stat.S_IRUSR | stat.S_IWUSR)  # 0o600
        tmp.replace(p)
    except Exception:
        p.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")

def _save_json(path: str, data: dict):
    _write_secure_json(path, data)

def load_profile_info() -> Dict[str, dict]:
    return _load_json(PROFILE_INFO_FILE)

def save_profile_info(data: Dict[str, dict]) -> None:
    _save_json(PROFILE_INFO_FILE, data)

def get_or_init_profile(info: Dict[str, dict], profile: str) -> dict:
    today = _today_str()
    entry = info.get(profile, {})
    level = int(entry.get("level", 1))
    date = entry.get("date")
    error = entry.get("error")
    if date != today:
        entry = {"date": today, "level": 2 if level == 2 else 1,
                 "totalSearchPC": 0, "totalSearchMobile": 0, "error": error}
    else:
        entry.setdefault("totalSearchPC", int(entry.get("totalSearchPC", 0)))
        entry.setdefault("totalSearchMobile", int(entry.get("totalSearchMobile", 0)))
        entry["level"] = 2 if level == 2 else 1
        entry["date"] = today
    info[profile] = entry
    return entry

def set_level(profile: str, level: int) -> None:
    info = load_profile_info()
    entry = get_or_init_profile(info, profile)
    entry["level"] = 2 if int(level) == 2 else 1
    info[profile] = entry
    save_profile_info(info)

def bump_profile_progress(profile: str, is_pc: bool, delta: int = 1) -> None:
    info = load_profile_info()
    entry = get_or_init_profile(info, profile)
    key = "totalSearchPC" if is_pc else "totalSearchMobile"
    entry[key] = int(entry.get(key, 0)) + int(delta)
    info[profile] = entry
    save_profile_info(info)

# ---------------- Profiles discovery ----------------
class BrowserProfileManager(ABC):
    def __init__(self, browser_name: str):
        self.browser_name = browser_name
        self.platform = platform.system()
        self.user_data_dir = self._get_user_data_dir()

    @abstractmethod
    def _get_user_data_dir(self) -> Optional[Path]:
        ...

    def get_available_profiles(self) -> List[Dict[str, str]]:
        if not self.user_data_dir or not self.user_data_dir.exists():
            return []
        out = []
        default_dir = self.user_data_dir / "Default"
        if default_dir.exists() and default_dir.is_dir():
            out.append("Default")
        for item in self.user_data_dir.iterdir():
            if item.is_dir() and item.name.startswith("Profile "):
                out.append(item.name)
        return [{"name": d, "directory": d, "path": str(self.user_data_dir / d)} for d in out]

class ChromeProfileManager(BrowserProfileManager):
    def __init__(self):
        super().__init__("chrome")
    def _get_user_data_dir(self) -> Optional[Path]:
        home = Path.home()
        if self.platform == "Windows":
            p = Path(os.environ.get("LOCALAPPDATA", "")) / "Google" / "Chrome" / "User Data"
            return p if p.exists() else None
        if self.platform == "Darwin":
            p = home / "Library" / "Application Support" / "Google" / "Chrome"
            return p if p.exists() else None
        if self.platform == "Linux":
            for name in ("google-chrome", "google-chrome-stable", "chromium", "chromium-browser"):
                p = home / ".config" / name
                if p.exists():
                    return p
        return None

class EdgeProfileManager(BrowserProfileManager):
    def __init__(self):
        super().__init__("edge")
    def _get_user_data_dir(self) -> Optional[Path]:
        home = Path.home()
        if self.platform == "Windows":
            p = Path(os.environ.get("LOCALAPPDATA", "")) / "Microsoft" / "Edge" / "User Data"
            return p if p.exists() else None
        if self.platform == "Darwin":
            p = home / "Library" / "Application Support" / "Microsoft Edge"
            return p if p.exists() else None
        if self.platform == "Linux":
            for name in ("microsoft-edge", "microsoft-edge-stable"):
                p = home / ".config" / name
                if p.exists():
                    return p
        return None

# ---------------- Launchers ----------------
def launch_browser(browser: str, profile_dir: Optional[str] = None) -> Optional[subprocess.Popen]:
    system = platform.system()
    try:
        if system == "Windows":
            if browser == "chrome":
                cmd = ["start", "chrome"]
                if profile_dir: cmd += [f"--profile-directory={profile_dir}"]
                return subprocess.Popen(cmd, shell=True)
            if browser == "edge":
                cmd = ["start", "msedge"]
                if profile_dir: cmd += [f"--profile-directory={profile_dir}"]
                return subprocess.Popen(cmd, shell=True)
            if browser == "firefox":
                return subprocess.Popen(["start", "firefox"], shell=True)
            if browser == "brave":
                return subprocess.Popen(["start", "brave"], shell=True)
        elif system == "Darwin":
            app_map = {"chrome": "Google Chrome", "edge": "Microsoft Edge",
                       "firefox": "Firefox", "brave": "Brave Browser"}
            app = app_map.get(browser)
            if app:
                args = ["open", "-a", app, "--args"]
                if profile_dir: args += [f"--profile-directory={profile_dir}"]
                return subprocess.Popen(args)
        else:
            if browser == "chrome":
                for c in ("google-chrome", "google-chrome-stable", "chromium-browser", "chromium"):
                    try:
                        cmd = [c]
                        if profile_dir: cmd += [f"--profile-directory={profile_dir}"]
                        return subprocess.Popen(cmd)
                    except FileNotFoundError:
                        pass
            if browser == "edge":
                for c in ("microsoft-edge", "microsoft-edge-stable"):
                    try:
                        cmd = [c]
                        if profile_dir: cmd += [f"--profile-directory={profile_dir}"]
                        return subprocess.Popen(cmd)
                    except FileNotFoundError:
                        pass
            if browser == "firefox":
                return subprocess.Popen(["firefox"])
            if browser == "brave":
                return subprocess.Popen(["brave-browser"])
    except Exception as e:
        print("Launch error:", e)
    return None

def launch_mobile_browser(browser: str, profile_dir: Optional[str] = None) -> Optional[subprocess.Popen]:
    """Fallback: desktop browser window with mobile UA + size."""
    system = platform.system()
    size_flag = f"--window-size={MOBILE_WINDOW_SIZE}"
    ua_flag   = f"--user-agent={MOBILE_UA}"
    try:
        if system == "Windows":
            if browser == "chrome":
                cmd = ["start", "chrome"]
                if profile_dir: cmd += [f"--profile-directory={profile_dir}"]
                cmd += [ua_flag, size_flag]
                return subprocess.Popen(cmd, shell=True)
            if browser == "edge":
                cmd = ["start", "msedge"]
                if profile_dir: cmd += [f"--profile-directory={profile_dir}"]
                cmd += [ua_flag, size_flag]
                return subprocess.Popen(cmd, shell=True)
        elif system == "Darwin":
            app_map = {"chrome": "Google Chrome", "edge": "Microsoft Edge"}
            app = app_map.get(browser)
            if app:
                args = ["open", "-a", app, "--args"]
                if profile_dir: args += [f"--profile-directory={profile_dir}"]
                args += [ua_flag, size_flag]
                return subprocess.Popen(args)
        else:
            if browser == "chrome":
                for c in ("google-chrome", "google-chrome-stable", "chromium-browser", "chromium"):
                    try:
                        cmd = [c]
                        if profile_dir: cmd += [f"--profile-directory={profile_dir}"]
                        cmd += [ua_flag, size_flag]
                        return subprocess.Popen(cmd)
                    except FileNotFoundError:
                        pass
            if browser == "edge":
                for c in ("microsoft-edge", "microsoft-edge-stable"):
                    try:
                        cmd = [c]
                        if profile_dir: cmd += [f"--profile-directory={profile_dir}"]
                        cmd += [ua_flag, size_flag]
                        return subprocess.Popen(cmd)
                    except FileNotFoundError:
                        pass
    except Exception as e:
        print("Mobile launch error:", e)
    return None

def close_browser_windows(browser: str):
    try:
        if platform.system() == "Windows":
            exe = "chrome.exe" if browser == "chrome" else "msedge.exe"
            subprocess.run(["taskkill", "/IM", exe, "/F"],
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    except Exception:
        pass

# ---------------- Pause helpers ----------------
def _wait_if_paused():
    while True:
        with state_lock:
            paused  = state.get("is_paused", False)
            running = state.get("is_running", False)
        if not running or not paused:
            return
        time.sleep(0.1)

def sleep_with_pause(seconds: float):
    end = time.time() + max(0.0, seconds)
    while time.time() < end:
        with state_lock:
            running = state.get("is_running", False)
        if not running:
            return
        _wait_if_paused()
        time.sleep(min(0.1, max(0.0, end - time.time())))

# ---------------- Query helpers ----------------
def _build_queries(base: List[str], count: int) -> List[str]:
    if not base:
        return []
    out, i = [], 0
    while len(out) < count:
        out.append(base[i % len(base)])
        i += 1
    return out[:count]

# ---------------- Playwright Mobile ----------------
class MobileSearcher:
    def __init__(self, profile: str, device_name: str = MOBILE_DEVICE, headless: bool = MOBILE_HEADLESS):
        self.profile = profile
        self.device_name = device_name
        self.headless = headless
        self._p = self._browser = self._context = None
        self.page = None
        self.storage_path = Path("sessions_mobile") / f"{profile}.json"
        if not self.storage_path.parent.exists():
            self.storage_path.parent.mkdir(parents=True, exist_ok=True)
    def __enter__(self):
        if not _HAVE_PLAYWRIGHT:
            raise RuntimeError("Playwright not available")
        self._p = sync_playwright().start()
        device = self._p.devices.get(self.device_name) or self._p.devices["Pixel 5"]
        self._browser = self._p.chromium.launch(headless=self.headless)
        self._context = self._browser.new_context(
            **device,
            locale=MOBILE_LOCALE,
            timezone_id=MOBILE_TIMEZONE,
            storage_state=str(self.storage_path) if self.storage_path.exists() else None
        )
        self.page = self._context.new_page()
        return self
    def __exit__(self, exc_type, exc, tb):
        try:
            if self._context:
                try:
                    self._context.storage_state(path=str(self.storage_path))
                except:
                    pass
        finally:
            try: self._context and self._context.close()
            except: pass
            try: self._browser and self._browser.close()
            except: pass
            try: self._p and self._p.stop()
            except: pass
    def _j(self,a,b): time.sleep(random.uniform(a,b))
    def _box(self)->str:
        sels = [
            "input[name='q']", "input[type='search']", "form[role='search'] input",
            "[aria-label='Enter your search term']", "[aria-label='Search'] input, [aria-label='Search']",
            "input#sb_form_q"
        ]
        for s in sels:
            if self.page.locator(s).first.count():
                return s
        return sels[0]
    def _type(self, sel: str, text: str):
        box = self.page.locator(sel).first
        box.click()
        typed=[]
        for ch in text:
            if typed and random.random()<0.06:
                self.page.keyboard.press("Backspace"); typed.pop(); time.sleep(random.uniform(0.04,0.12))
            self.page.keyboard.type(ch, delay=random.uniform(35,120)); typed.append(ch)
            if random.random()<0.07: time.sleep(random.uniform(0.08,0.25))
        time.sleep(random.uniform(0.15,0.6))
    def _scroll(self, a=2, b=6):
        for _ in range(random.randint(a,b)):
            self.page.evaluate(f"window.scrollBy(0, {random.randint(200,800)});")
            time.sleep(random.uniform(0.5,1.6))
        if random.random()<0.3:
            self.page.evaluate("window.scrollBy(0, -300);")
            time.sleep(random.uniform(0.4,1.0))
    def _maybe_click(self, prob=0.4, dwell_min=5, dwell_max=18):
        if random.random()>prob:
            return
        cands = self.page.locator("li.b_algo h2 a, li[data-h*='b_algo'] h2 a, .b_algo h2 a, #b_results a").all()
        if not cands:
            return
        link = random.choice(cands[:min(len(cands),6)])
        try:
            with self.page.expect_navigation(timeout=15000):
                link.click()
            self._j(dwell_min, dwell_max); self._scroll(1,3)
            if random.random()<0.25:
                anchors = self.page.locator("a[href]").all()
                if anchors:
                    random.choice(anchors[:min(len(anchors),8)]).click(timeout=5000)
                    self._j(3,10); self._scroll(1,3)
            if random.random()<0.8:
                self.page.go_back(timeout=15000); self._j(1.2,3.0)
        except PWTimeout:
            pass
    def search(self, query: str, click_probability: float = 0.4):
        self.page.goto("https://www.bing.com/", wait_until="domcontentloaded", timeout=30000)
        self._j(0.6,1.6)
        for sel in ["button#bnp_btn_accept","button[aria-label*='Accept']","text=I agree","text=Accept all","text=Accept"]:
            try:
                if self.page.locator(sel).first.is_visible():
                    self.page.locator(sel).first.click(timeout=1500)
                    break
            except:
                pass
        box = self._box()
        self._type(box, query)
        self.page.keyboard.press("Enter")
        self.page.wait_for_selector("#b_results, .b_algo, [aria-label*='Results']", timeout=15000)
        self._j(1.0,2.2); self._scroll(2,6); self._maybe_click(click_probability)

# ---------------- Worker ----------------
def automation_worker(fruits: List[str], delay: float, browser: str, profiles: List[Dict[str, str]]):
    global state
    if not profiles:
        profiles = [{"name": "Default", "directory": None, "path": None}]
    completed = 0
    browser_processes = []
    completed_profiles = []
    MAX_ACTIVE_BROWSERS = 2

    try:
        for profile in profiles:
            with state_lock:
                running = state["is_running"]
            if not running:
                break

            name = profile["name"]
            directory = profile.get("directory")

            # Desktop pass
            with state_lock:
                state["current_profile"] = name
                state["status"] = f"Opening {browser} for profile: {name}"
                pp = state.get("profile_progress", {})
                desktop_total = pp.get(name, {}).get("total", len(fruits))

            proc = launch_browser(browser, directory)
            if proc:
                browser_processes.append((proc, name))
            sleep_with_pause(3)

            queries = _build_queries(fruits, desktop_total)
            for q in queries:
                _wait_if_paused()
                with state_lock:
                    running = state["is_running"]
                if not running:
                    break

                with state_lock:
                    state["current_search"] = q
                    state["status"] = f"Searching for: {q}"

                try:
                    pyautogui.hotkey("ctrl","t"); sleep_with_pause(0.5)
                    pyautogui.hotkey("ctrl","l"); sleep_with_pause(0.3)
                    pyautogui.typewrite(q, interval=0.05); sleep_with_pause(0.2)
                    pyautogui.press("enter")
                except Exception as e:
                    print("pyautogui error:", e)

                completed += 1
                with state_lock:
                    state["completed"] = completed
                    total = max(1, state.get("total", 1))
                    state["progress"] = (completed / total) * 100.0
                    pp = state.get("profile_progress", {})
                    if name in pp:
                        pp[name]["done"] = min(pp[name]["done"] + 1, pp[name]["total"])

                try:
                    bump_profile_progress(name, is_pc=True, delta=1)
                except:
                    pass

                sleep_with_pause(max(0.1, delay + random.uniform(0.15, 0.6)))

            completed_profiles.append(name)

            # Keep windows manageable
            if len(completed_profiles) > MAX_ACTIVE_BROWSERS:
                try:
                    close_browser_windows(browser)
                    if browser_processes:
                        oldest_process, _ = browser_processes.pop(0)
                        try:
                            oldest_process.terminate(); sleep_with_pause(0.5)
                            if oldest_process.poll() is None:
                                oldest_process.kill()
                        except:
                            pass
                    completed_profiles.pop(0)
                    sleep_with_pause(2)
                    with state_lock:
                        state["status"] = f"Freed memory, continuing with {name}"
                except Exception as e:
                    print("Memory mgmt error:", e)

            # Mobile pass (Playwright preferred)
            with state_lock:
                elig = state.get("profile_eligibility", {}).get(name, {})
                mobile_enabled = bool(state.get("mobile_enabled", False))
                mprog = state.setdefault("mobile_progress", {})
                cur = mprog.get(name)

            mobile_ok = mobile_enabled and bool(elig.get("mobile")) and MOBILE_SEARCH_COUNT > 0
            if not mobile_ok:
                continue

            with state_lock:
                if cur is None:
                    state["mobile_progress"][name] = {"done": 0, "total": MOBILE_SEARCH_COUNT}

            mqueries = _build_queries(fruits, MOBILE_SEARCH_COUNT) or \
                       ["weather today", "top news", "sports scores", "nearby restaurants", "time now"]

            if _HAVE_PLAYWRIGHT:
                try:
                    with state_lock:
                        state["status"] = f"Opening Playwright mobile for: {name}"
                    with MobileSearcher(profile=name) as mobile:
                        for mq in mqueries:
                            _wait_if_paused()
                            with state_lock:
                                running = state["is_running"]
                            if not running:
                                break

                            with state_lock:
                                state["current_search"] = f"{mq} [m]"
                                state["status"] = f"Mobile searching: {mq}"

                            try:
                                mobile.search(mq, click_probability=0.4)
                            except Exception as e:
                                print("Playwright mobile error:", e)

                            with state_lock:
                                mp = state.setdefault("mobile_progress", {}).setdefault(
                                    name, {"done": 0, "total": MOBILE_SEARCH_COUNT}
                                )
                                mp["done"] = min(mp["done"] + 1, mp["total"])
                            try:
                                bump_profile_progress(name, is_pc=False, delta=1)
                            except:
                                pass
                            sleep_with_pause(max(0.1, delay + random.uniform(0.3, 0.9)))
                except Exception as e:
                    print("Playwright init/runtime failed, fallback:", e)
                else:
                    continue  # finished mobile via PW

            # Fallback: mobile UA window
            with state_lock:
                state["status"] = f"Opening {browser} (mobile UA) for profile: {name}"
            mproc = launch_mobile_browser(browser, directory)
            if mproc:
                browser_processes.append((mproc, f"{name} (mobile)"))
            sleep_with_pause(3)

            for mq in mqueries:
                _wait_if_paused()
                with state_lock:
                    running = state["is_running"]
                if not running:
                    break

                with state_lock:
                    state["current_search"] = f"{mq} [m]"
                    state["status"] = f"Mobile searching: {mq}"

                try:
                    pyautogui.hotkey("ctrl","t"); sleep_with_pause(0.5)
                    pyautogui.hotkey("ctrl","l"); sleep_with_pause(0.3)
                    pyautogui.typewrite(mq, interval=0.05); sleep_with_pause(0.2)
                    pyautogui.press("enter")
                except Exception as e:
                    print("pyautogui error (mobile fallback):", e)

                with state_lock:
                    mp = state.setdefault("mobile_progress", {}).setdefault(
                        name, {"done": 0, "total": MOBILE_SEARCH_COUNT}
                    )
                    mp["done"] = min(mp["done"] + 1, mp["total"])
                try:
                    bump_profile_progress(name, is_pc=False, delta=1)
                except:
                    pass
                sleep_with_pause(max(0.1, delay + random.uniform(0.15, 0.6)))

    except pyautogui.FailSafeException:
        print("Failsafe: mouse moved to top-left. Stopping.")
    except Exception as e:
        print("Automation error:", e)
    finally:
        try:
            for process, _ in browser_processes:
                if process:
                    try:
                        process.terminate(); sleep_with_pause(0.5)
                        if process.poll() is None:
                            process.kill()
                    except:
                        pass
        finally:
            with state_lock:
                state["is_running"] = False
                state["status"] = "Automation completed" if state["completed"] >= state["total"] else "Automation stopped"
                state["current_search"] = ""
                state["current_profile"] = ""
                state["is_paused"] = False

# ---------------- Routes ----------------
@app.route("/")
def index():
    return app.send_static_file("index.html")

@app.route("/api/profiles/<browser>", methods=["GET"])
def get_browser_profiles(browser):
    b = (browser or "").lower()
    if b == "chrome":
        mgr = ChromeProfileManager()
    elif b == "edge":
        mgr = EdgeProfileManager()
    else:
        return jsonify({"profiles": []})
    return jsonify({"profiles": mgr.get_available_profiles()})

@app.route("/api/profiles", methods=["GET"])
def get_profiles():
    return jsonify({"profiles": ChromeProfileManager().get_available_profiles()})

@app.route("/api/levels", methods=["GET"])
def api_get_levels():
    info = load_profile_info()
    changed = False
    for k in list(info.keys()):
        norm = get_or_init_profile(info, k)
        if norm is not info[k]:
            changed = True
    if changed:
        save_profile_info(info)
    return jsonify({"levels": {k: int(v.get("level", 1)) for k, v in info.items()}})

@app.route("/api/levels", methods=["POST"])
def api_set_level():
    data = request.json or {}
    profile = data.get("profile")
    level = int(data.get("level", 1))
    if not profile:
        return jsonify({"error": "missing profile"}), 400
    if level not in (1, 2):
        return jsonify({"error": "level must be 1 or 2"}), 400
    set_level(profile, level)
    return jsonify({"profile": profile, "level": level})

@app.route("/api/profile-info", methods=["GET"])
def api_profile_info():
    info = load_profile_info()
    for k in list(info.keys()):
        get_or_init_profile(info, k)
    save_profile_info(info)
    return jsonify(info)

@app.route("/api/save", methods=["POST"])
def save_fruits():
    data = request.json or {}
    fruits = data.get("fruits", [])
    ok = _save_safe("fruits.json", fruits)
    return jsonify({"message": f"Saved {len(fruits)} fruits" if ok else "Save failed"})

@app.route("/api/load", methods=["GET"])
def load_fruits():
    fruits = _load_safe("fruits.json")
    return jsonify({"fruits": fruits if fruits is not None else []})

# ---------- Start/Stop ----------
@app.route("/api/start", methods=["POST"])
def start_automation():
    global worker_thread
    with state_lock:
        if state["is_running"]:
            return jsonify({"error": "Automation is already running"}), 400

    data = request.json or {}
    fruits: List[str] = data.get("fruits", [])
    delay = float(data.get("delay", 3.0))
    browser = data.get("browser", "edge")
    req_profiles = data.get("selectedProfiles", [])
    use_default = data.get("useDefaultIfNoProfile", False)
    mobile_enabled = bool(data.get("mobileEnabled", False))

    if not fruits:
        return jsonify({"error": "No fruits provided"}), 400
    if delay < 0.5:
        delay = 3.0

    # Resolve profiles
    profiles: List[Dict[str, Any]] = []
    if browser in ["chrome", "edge"]:
        if req_profiles:
            profiles = req_profiles
            selected_profiles_memory[browser] = req_profiles
        elif selected_profiles_memory.get(browser):
            profiles = selected_profiles_memory[browser]
        elif use_default:
            profiles = [{"name": "Default", "directory": "Default", "path": "Default"}]
    if not profiles:
        profiles = [{"name": "Default", "directory": None, "path": None}]

    # Totals by Level
    profile_progress: Dict[str, Dict[str, int]] = {}
    profile_points: Dict[str, Dict[str, Any]] = {}
    profile_eligibility: Dict[str, Dict[str, Any]] = {}
    mobile_progress: Dict[str, Dict[str, int]] = {}
    total_searches = 0

    info = load_profile_info()
    for p in profiles:
        name = p.get("name") or p.get("directory") or "Default"
        entry = get_or_init_profile(info, name)
        lvl = int(entry.get("level", 1))
        desktop_total = 10 if lvl == 1 else 32
        mobile_ok = (lvl == 2)
        profile_progress[name] = {"done": 0, "total": desktop_total}
        profile_points[name] = {"points": None, "level": lvl, "last_updated": int(time.time())}
        profile_eligibility[name] = {"mobile": mobile_ok, "reason": "" if mobile_ok else "level < 2"}
        total_searches += desktop_total
        if mobile_enabled and mobile_ok and MOBILE_SEARCH_COUNT > 0:
            mobile_progress[name] = {"done": 0, "total": MOBILE_SEARCH_COUNT}
    save_profile_info(info)

    with state_lock:
        state.update({
            "is_running": True, "status": "Starting automation...",
            "progress": 0.0, "completed": 0, "total": total_searches,
            "is_paused": False, "profile_progress": profile_progress,
            "profile_points": {**state.get("profile_points", {}), **profile_points},
            "profile_eligibility": profile_eligibility,
            "mobile_enabled": mobile_enabled, "mobile_progress": mobile_progress
        })

    def worker():
        try:
            automation_worker(fruits, delay, browser, profiles)
        finally:
            with state_lock:
                state["is_running"] = False

    worker_thread = threading.Thread(target=worker, daemon=True)
    worker_thread.start()

    return jsonify({
        "message": "Automation started",
        "browser": browser.capitalize(),
        "profiles_in_use": [p["name"] for p in profiles],
        "total_searches": total_searches,
        "mobile_enabled": mobile_enabled,
        "mobile_search_count": MOBILE_SEARCH_COUNT,
        "playwright": _HAVE_PLAYWRIGHT
    }), 202

@app.route("/api/stop", methods=["POST"])
def stop_automation():
    with state_lock:
        if state["is_running"]:
            state["is_running"] = False
            state["status"] = "Stopping automation..."
    return jsonify({"message": "Stopping"})

@app.route("/api/pause", methods=["POST"])
def pause_automation():
    with state_lock:
        if state["is_running"]:
            state["is_paused"] = True
            state["status"] = "Paused"
    return jsonify({"message": "Paused"})

@app.route("/api/resume", methods=["POST"])
def resume_automation():
    with state_lock:
        if state["is_running"]:
            state["is_paused"] = False
            state["status"] = "Resuming..."
    return jsonify({"message": "Resumed"})

@app.route("/api/rewards", methods=["GET"])
def get_rewards_cache():
    return jsonify({"available": False, "profiles": state.get("profile_points", {})})

@app.route("/api/rewards/refresh", methods=["POST"])
def refresh_rewards():
    return jsonify({"error": "Rewards scraping disabled in this build"}), 501

@app.route("/api/status", methods=["GET"])
def get_status():
    with state_lock:
        return jsonify({
            "is_running": state["is_running"],
            "status": state["status"],
            "current_search": state["current_search"],
            "current_profile": state["current_profile"],
            "progress": round(state["progress"], 1),
            "completed": state["completed"],
            "total": state["total"],
            "is_paused": state.get("is_paused", False),
            "profile_progress": state.get("profile_progress", {}),
            "profile_points": state.get("profile_points", {}),
            "profile_eligibility": state.get("profile_eligibility", {}),
            "mobile_enabled": state.get("mobile_enabled", False),
            "mobile_progress": state.get("mobile_progress", {}),
            "mobile_search_count": MOBILE_SEARCH_COUNT,
            "playwright": _HAVE_PLAYWRIGHT
        })

@app.route("/api/health", methods=["GET"])
def health_check():
    chrome_dir = ChromeProfileManager().user_data_dir
    edge_dir   = EdgeProfileManager().user_data_dir
    return jsonify({
        "status": "healthy",
        "platform": platform.system(),
        "chrome_dir": str(chrome_dir) if chrome_dir else None,
        "edge_dir": str(edge_dir) if edge_dir else None,
        "mobile_search_count": MOBILE_SEARCH_COUNT,
        "playwright": _HAVE_PLAYWRIGHT
    })

# ---------------- AI CONFIG: store/retrieve API keys (server-side) ----------------
def _load_ai_config() -> dict:
    cfg = _load_json(AI_CONFIG_FILE)
    cfg.setdefault("provider", "auto")  # auto|gemini|openai
    cfg.setdefault("gemini", {"api_key": "", "model": "gemini-1.5-flash"})
    cfg.setdefault("openai", {"api_key": "", "model": "gpt-4o-mini"})
    return cfg

def _save_ai_config(cfg: dict):
    _write_secure_json(AI_CONFIG_FILE, cfg)

@app.route("/api/ai-config", methods=["GET"])
def api_get_ai_config():
    """
    Returns non-sensitive config only (never returns the API keys).
    """
    cfg = _load_ai_config()
    has_gemini = bool(os.getenv("GEMINI_API_KEY") or cfg.get("gemini", {}).get("api_key"))
    has_openai = bool(os.getenv("OPENAI_API_KEY") or cfg.get("openai", {}).get("api_key"))
    return jsonify({
        "provider": cfg.get("provider", "auto"),
        "gemini": {"has_key": has_gemini, "model": cfg.get("gemini", {}).get("model", "gemini-1.5-flash")},
        "openai": {"has_key": has_openai, "model": cfg.get("openai", {}).get("model", "gpt-4o-mini")}
    })

@app.route("/api/ai-config", methods=["POST"])
def api_set_ai_config():
    """
    Body:
      {
        "provider": "gemini" | "openai" | "auto",   # optional
        "vendor": "gemini" | "openai",              # which vendor you're setting
        "apiKey": "....",                           # optional: saved server-side if present
        "model": "gemini-2.5-pro",                  # optional
        "clear": false                               # optional: if true, deletes saved key
      }
    """
    data = request.json or {}
    cfg = _load_ai_config()

    provider = data.get("provider")
    vendor   = (data.get("vendor") or "").lower()
    api_key  = (data.get("apiKey") or "").strip()
    model    = (data.get("model") or "").strip()
    clear    = bool(data.get("clear", False))

    if provider in ("auto", "gemini", "openai"):
        cfg["provider"] = provider

    if vendor in ("gemini", "openai"):
        if clear:
            cfg[vendor]["api_key"] = ""
        elif api_key:
            cfg[vendor]["api_key"] = api_key
        if model:
            cfg[vendor]["model"] = model

    _save_ai_config(cfg)
    return jsonify({"ok": True})

# ---------------- AI Query Generation (Gemini / OpenAI / Fallback) ----------------
def _fallback_generate_queries(seed: str, count: int = 30) -> list:
    seed = (seed or "interesting topics").strip()
    templates = [
        "what is {}", "how to {}", "best {} tips", "latest {} news", "{} 2025",
        "is {} worth it", "{} near me", "{} vs alternatives", "beginner guide to {}",
        "advanced {} techniques", "cheap {} ideas", "top {} mistakes", "can you {}",
        "why is {} important", "where to learn {}", "fast way to {}", "{} for beginners",
        "{} for experts", "common {} questions", "{} step by step", "simple {} tricks",
        "pro {} settings", "daily {} routine", "safe way to {}", "local {} updates",
        "{} examples", "explain {} like I'm five", "best free {} tools",
        "{} troubleshooting", "{} tutorial"
    ]
    parts = [p.strip() for p in seed.replace(" and ", ",").split(",") if p.strip()] or [seed]
    out, i = [], 0
    while len(out) < max(1, count):
        phrase = parts[i % len(parts)]; t = templates[i % len(templates)]
        out.append(t.format(phrase)); i += 1
    seen, dedup = set(), []
    for q in out:
        lk = q.lower()
        if lk not in seen:
            seen.add(lk); dedup.append(q)
    return dedup[:count]

def _openai_generate_queries(seed: str, count: int, api_key: str, model: str) -> list:
    try:
        from openai import OpenAI  # pip install openai
    except Exception:
        return _fallback_generate_queries(seed, count)
    try:
        client = OpenAI(api_key=api_key or None)
        use_model = model or os.getenv("OPENAI_MODEL", "gpt-4o-mini")
        sys_prompt = (
            "You are a search query generator. The user gives ONE line describing a topic.\n"
            f"Return EXACTLY {count} distinct, human-like web search queries, one per line. "
            "Vary intent (how-to, what/why, comparisons, near me, news/today), vary lengths; "
            "no numbering; no extra commentary."
        )
        resp = client.chat.completions.create(
            model=use_model,
            messages=[
                {"role": "system", "content": sys_prompt},
                {"role": "user", "content": (seed or '').strip() or "general interesting topics"},
            ],
            temperature=0.8, top_p=0.9,
        )
        text = (resp.choices[0].message.content or "").strip()
        lines = [ln.strip().lstrip("-•0123456789. ").strip() for ln in text.splitlines() if ln.strip()]
        uniq, seen = [], set()
        for q in lines:
            lk = q.lower()
            if lk and lk not in seen:
                uniq.append(q); seen.add(lk)
        if not uniq:
            return _fallback_generate_queries(seed, count)
        if len(uniq) < count:
            uniq += _fallback_generate_queries(seed, count - len(uniq))
        return uniq[:count]
    except Exception as e:
        print("OpenAI generation failed:", e)
        return _fallback_generate_queries(seed, count)

def _gemini_generate_queries(seed: str, count: int, api_key: str, model: str) -> list:
    try:
        import google.generativeai as genai  # pip install google-generativeai
    except Exception:
        return _fallback_generate_queries(seed, count)
    try:
        use_key = api_key or os.getenv("GEMINI_API_KEY") or ""
        if not use_key:
            return _fallback_generate_queries(seed, count)
        genai.configure(api_key=use_key)
        use_model = model or os.getenv("GEMINI_MODEL", "gemini-1.5-flash")

        instruction = (
            f"Return EXACTLY {count} distinct, human-like web search queries, one per line. "
            "Vary intent (how-to, what/why, comparisons, near me, news/today) and vary lengths. "
            "No numbering or extra commentary."
        )
        content = f"{instruction}\n\nTOPIC: {(seed or '').strip() or 'general interesting topics'}"
        model_obj = genai.GenerativeModel(use_model)
        resp = model_obj.generate_content(content)
        text = (getattr(resp, 'text', None) or "").strip()

        lines = [ln.strip().lstrip('-•0123456789. ').strip() for ln in text.splitlines() if ln.strip()]
        uniq, seen = [], set()
        for q in lines:
            lk = q.lower()
            if lk and lk not in seen:
                uniq.append(q); seen.add(lk)
        if not uniq:
            return _fallback_generate_queries(seed, count)
        if len(uniq) < count:
            uniq += _fallback_generate_queries(seed, count - len(uniq))
        return uniq[:count]
    except Exception as e:
        print("Gemini generation failed:", e)
        return _fallback_generate_queries(seed, count)

def _choose_provider_and_generate(prompt: str, count: int, preferred: Optional[str] = None) -> (list, str):
    """
    Decide which provider to use (env or saved config), generate, and return (queries, provider_used).
    """
    cfg = _load_ai_config()

    # Preferred provider from request or config/env
    if preferred not in ("gemini", "openai", "auto", None):
        preferred = None
    provider = (preferred or cfg.get("provider", "auto") or os.getenv("AI_PROVIDER", "auto")).lower()

    # Merge keys/models from env and saved file (env wins)
    gem_key   = os.getenv("GEMINI_API_KEY") or cfg.get("gemini", {}).get("api_key", "")
    gem_model = os.getenv("GEMINI_MODEL")     or cfg.get("gemini", {}).get("model", "gemini-1.5-flash")
    oa_key    = os.getenv("OPENAI_API_KEY") or cfg.get("openai", {}).get("api_key", "")
    oa_model  = os.getenv("OPENAI_MODEL")     or cfg.get("openai", {}).get("model", "gpt-4o-mini")

    if provider == "gemini" and gem_key:
        return _gemini_generate_queries(prompt, count, gem_key, gem_model), "gemini"
    if provider == "openai" and oa_key:
        return _openai_generate_queries(prompt, count, oa_key, oa_model), "openai"

    # auto-pick
    if gem_key:
        return _gemini_generate_queries(prompt, count, gem_key, gem_model), "gemini"
    if oa_key:
        return _openai_generate_queries(prompt, count, oa_key, oa_model), "openai"

    return _fallback_generate_queries(prompt, count), "fallback"

@app.route("/api/ai-generate", methods=["POST"])
def api_ai_generate():
    """
    Body: { "prompt": str, "count": int(1..200), "save": bool, "provider"?: "gemini"|"openai"|"auto" }
    - If provider omitted, uses configured provider (or auto).
    - Keys come from env if present, otherwise from ai_config.json saved via /api/ai-config.
    """
    data = request.json or {}
    prompt   = (data.get("prompt") or "").strip()
    count    = max(1, min(200, int(data.get("count") or 30)))
    save     = bool(data.get("save", False))
    provider = (data.get("provider") or "").lower() or None

    fruits, used = _choose_provider_and_generate(prompt, count, preferred=provider)

    if save:
        _save_safe("fruits.json", fruits)
    return jsonify({"fruits": fruits, "saved": save, "provider": used})

# ---------------- Utils ----------------
def _save_safe(filename: str, data: Any) -> bool:
    try:
        with open(filename, "w", encoding="utf-8") as f:
            json.dump(data, f)
        return True
    except Exception as e:
        print("Save error:", e)
        return False

def _load_safe(filename: str) -> Optional[Any]:
    try:
        if not os.path.exists(filename):
            return None
        with open(filename, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception as e:
        print("Load error:", e)
        return None

if __name__ == "__main__":
    try:
        pyautogui.FAILSAFE = True
        pyautogui.PAUSE = 0.05
    except Exception:
        pass
    app.run(host="127.0.0.1", port=5000, debug=True)
