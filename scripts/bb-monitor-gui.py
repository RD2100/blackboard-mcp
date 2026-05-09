#!/usr/bin/env python3
"""Blackboard Monitor GUI v4 — 实时仪表盘

用法: python bb-monitor-gui.py [项目目录]
例:   python bb-monitor-gui.py D:\travel_app
"""

import json
import os
import sys
import threading
import time
from datetime import datetime, timezone

import tkinter as tk
from tkinter import ttk

# ─── 配置 ───

REFRESH_INTERVAL = 2  # 秒

# ─── 颜色主题（Tokyo Night）───

BG = "#1a1b26"
BG_CARD = "#24283b"
BG_ROW = "#1f2335"
FG = "#c0caf5"
FG_DIM = "#565f89"
GREEN = "#9ece6a"
YELLOW = "#e0af68"
RED = "#f7768e"
BLUE = "#7aa2f7"
CYAN = "#7dcfff"
PURPLE = "#bb9af7"


def find_state_file(project_dir=None):
    candidates = []
    if project_dir and os.path.isdir(project_dir):
        candidates.append(os.path.join(project_dir, ".claude", "blackboard", "state.json"))
    candidates.append(os.path.join(os.path.expanduser("~"), ".claude", "blackboard", "state.json"))
    for p in candidates:
        if os.path.isfile(p):
            return p
    return candidates[0] if candidates else ""


def load_state(path):
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except (json.JSONDecodeError, OSError):
        return None


class BlackboardMonitor:
    def __init__(self, root, state_file):
        self.root = root
        self.state_file = state_file
        self.sort_key = "heartbeat"  # 默认按心跳时间排序
        self.sort_reverse = True  # 最新的在前

        self.root.title("Blackboard Monitor v4")
        self.root.configure(bg=BG)
        self.root.geometry("780x620")
        self.root.minsize(640, 420)

        # 启动时强制置前
        self.root.attributes("-topmost", True)
        self.root.after(500, lambda: self.root.attributes("-topmost", False))

        self._build_ui()
        self._running = True
        self._thread = threading.Thread(target=self._refresh_loop, daemon=True)
        self._thread.start()
        self.root.protocol("WM_DELETE_WINDOW", self._on_close)

    def _build_ui(self):
        # 标题栏
        title_frame = tk.Frame(self.root, bg=BG)
        title_frame.pack(fill="x", padx=12, pady=(10, 4))
        tk.Label(title_frame, text="Blackboard Monitor", font=("Segoe UI", 16, "bold"),
                 fg=BLUE, bg=BG).pack(side="left")
        tk.Label(title_frame, text="v4", font=("Segoe UI", 10), fg=FG_DIM, bg=BG).pack(side="left", padx=(6, 0))
        self.time_label = tk.Label(title_frame, text="", font=("Segoe UI", 9), fg=FG_DIM, bg=BG)
        self.time_label.pack(side="right")

        # 指标卡片
        cards = tk.Frame(self.root, bg=BG)
        cards.pack(fill="x", padx=12, pady=6)

        self.card_sessions = self._make_card(cards, "Sessions", "0", BLUE, 0)
        self.card_files = self._make_card(cards, "Files", "0", GREEN, 1)
        self.card_locks = self._make_card(cards, "Locks", "0", YELLOW, 2)
        self.card_knowledge = self._make_card(cards, "Knowledge", "0", PURPLE, 3)

        # Session列表 + 排序按钮
        list_frame = tk.Frame(self.root, bg=BG)
        list_frame.pack(fill="both", expand=True, padx=12, pady=(2, 4))

        # 标题行：标题 + 排序按钮
        header = tk.Frame(list_frame, bg=BG)
        header.pack(fill="x")
        tk.Label(header, text="Sessions", font=("Segoe UI", 11, "bold"),
                 fg=FG, bg=BG).pack(side="left")

        sort_frame = tk.Frame(header, bg=BG)
        sort_frame.pack(side="right")

        self.sort_buttons = {}
        for key, label in [("name", "Name"), ("task", "Task"), ("heartbeat", "Time"), ("status", "Status")]:
            btn = tk.Button(sort_frame, text=label, font=("Segoe UI", 8),
                            bg=BG_CARD, fg=FG_DIM, activebackground=BG_ROW, activeforeground=FG,
                            relief="flat", bd=0, padx=6, pady=1,
                            command=lambda k=key: self._set_sort(k))
            btn.pack(side="left", padx=1)
            self.sort_buttons[key] = btn
        self._highlight_sort_button()

        # Treeview
        cols = ("status", "name", "task", "files", "heartbeat")
        self.tree = ttk.Treeview(list_frame, columns=cols, show="headings", height=6)
        self.tree.heading("status", text="")
        self.tree.heading("name", text="Name")
        self.tree.heading("task", text="Task")
        self.tree.heading("files", text="Files")
        self.tree.heading("heartbeat", text="Heartbeat")
        self.tree.column("status", width=30, minwidth=30, anchor="center")
        self.tree.column("name", width=100, minwidth=60)
        self.tree.column("task", width=300, minwidth=120)
        self.tree.column("files", width=50, minwidth=40, anchor="center")
        self.tree.column("heartbeat", width=120, minwidth=80)

        style = ttk.Style()
        style.theme_use("clam")
        style.configure("Treeview", background=BG_CARD, foreground=FG, fieldbackground=BG_CARD,
                         rowheight=26, font=("Segoe UI", 9))
        style.configure("Treeview.Heading", background=BG_ROW, foreground=FG_DIM,
                         font=("Segoe UI", 9, "bold"))
        style.map("Treeview", background=[("selected", BG_ROW)])

        scrollbar = ttk.Scrollbar(list_frame, orient="vertical", command=self.tree.yview)
        self.tree.configure(yscrollcommand=scrollbar.set)
        self.tree.pack(side="left", fill="both", expand=True)
        scrollbar.pack(side="right", fill="y")

        # 底部面板
        bottom = tk.Frame(self.root, bg=BG)
        bottom.pack(fill="both", expand=True, padx=12, pady=(2, 10))

        # 知识面板
        kn_frame = tk.Frame(bottom, bg=BG)
        kn_frame.pack(side="left", fill="both", expand=True, padx=(0, 4))
        tk.Label(kn_frame, text="Knowledge & Decisions", font=("Segoe UI", 10, "bold"),
                 fg=PURPLE, bg=BG).pack(anchor="w")
        self.kn_text = tk.Text(kn_frame, bg=BG_CARD, fg=FG, font=("Consolas", 9),
                                height=6, wrap="word", relief="flat", bd=0,
                                insertbackground=FG, selectbackground=BG_ROW)
        self.kn_text.pack(fill="both", expand=True)

        # 健康检查
        health_frame = tk.Frame(bottom, bg=BG, width=180)
        health_frame.pack(side="right", fill="y", padx=(4, 0))
        health_frame.pack_propagate(False)
        tk.Label(health_frame, text="Health", font=("Segoe UI", 10, "bold"),
                 fg=CYAN, bg=BG).pack(anchor="w")
        self.health_text = tk.Text(health_frame, bg=BG_CARD, fg=FG, font=("Consolas", 9),
                                    height=6, wrap="word", relief="flat", bd=0, width=20,
                                    insertbackground=FG, selectbackground=BG_ROW)
        self.health_text.pack(fill="both", expand=True)

    def _make_card(self, parent, title, value, color, col):
        frame = tk.Frame(parent, bg=BG_CARD, padx=10, pady=6)
        frame.grid(row=0, column=col, padx=3, sticky="nsew")
        parent.columnconfigure(col, weight=1)
        tk.Label(frame, text=title, font=("Segoe UI", 9), fg=FG_DIM, bg=BG_CARD).pack(anchor="w")
        val_label = tk.Label(frame, text=value, font=("Segoe UI", 18, "bold"), fg=color, bg=BG_CARD)
        val_label.pack(anchor="w")
        return val_label

    def _set_sort(self, key):
        if self.sort_key == key:
            self.sort_reverse = not self.sort_reverse
        else:
            self.sort_key = key
            self.sort_reverse = (key == "heartbeat")  # 时间默认倒序，其他默认正序
        self._highlight_sort_button()

    def _highlight_sort_button(self):
        for key, btn in self.sort_buttons.items():
            if key == self.sort_key:
                arrow = " ↓" if self.sort_reverse else " ↑"
                btn.config(fg=BLUE, text=key.capitalize() + arrow, font=("Segoe UI", 8, "bold"))
            else:
                btn.config(fg=FG_DIM, text=key.capitalize(), font=("Segoe UI", 8))

    def _refresh_loop(self):
        while self._running:
            self.root.after(0, self._update)
            time.sleep(REFRESH_INTERVAL)

    def _sort_sessions(self, sessions):
        """按当前排序键排序session列表"""
        def sort_fn(item):
            sid, s = item
            key = self.sort_key
            if key == "name":
                return s.get("display_name", s.get("name", sid)).lower()
            elif key == "task":
                return s.get("task", "").lower()
            elif key == "heartbeat":
                return s.get("heartbeat", "")
            elif key == "status":
                order = {"active": 0, "stale": 1, "ended": 2}
                return order.get(s.get("status", ""), 3)
            return ""
        return sorted(sessions.items(), key=sort_fn, reverse=self.sort_reverse)

    def _update(self):
        state = load_state(self.state_file)
        if not state:
            self.time_label.config(text="state.json not found")
            return

        now = datetime.now(timezone.utc)
        self.time_label.config(text=datetime.now().strftime("%H:%M:%S"))

        # Sessions
        sessions = state.get("sessions", {})
        active = [s for s in sessions.values() if s.get("status") == "active"]
        stale_count = sum(1 for s in sessions.values() if s.get("status") == "stale")
        self.card_sessions.config(text=f"{len(active)}" + (f" +{stale_count} stale" if stale_count else ""))

        # Files
        file_count = len(state.get("file_registry", {}))
        self.card_files.config(text=str(file_count))

        # Locks
        lock_count = len(state.get("build_locks", {}))
        self.card_locks.config(text=str(lock_count))

        # Knowledge
        kn = state.get("knowledge", {})
        decs = len(state.get("decisions", []))
        bps = len(state.get("bug_patterns", []))
        high = sum(1 for k in kn.values() if k.get("confidence", 0) >= 0.8)
        self.card_knowledge.config(text=f"{len(kn)}" + (f" ({high}★)" if high else ""))

        # Session列表（排序后）
        for item in self.tree.get_children():
            self.tree.delete(item)

        for sid, s in self._sort_sessions(sessions):
            status = s.get("status", "?")
            task = s.get("task", "")
            dot = {"active": "●", "stale": "◐", "ended": "○"}.get(status, "?")
            no_task = not task or "未设置" in task or task.strip() == ""
            color_tag = "st_no_task" if (status == "active" and no_task) else f"st_{status}"

            # 显示名：优先display_name
            display_name = s.get("display_name", s.get("name", sid))
            if display_name == sid:
                display_name = sid[:8]  # UUID截断显示

            hb = s.get("heartbeat", "")
            try:
                hb_dt = datetime.fromisoformat(hb.replace("Z", "+00:00"))
                ago = int((now - hb_dt).total_seconds() / 60)
                hb_display = f"{ago}m ago" if ago < 60 else f"{ago // 60}h ago"
            except (ValueError, KeyError):
                hb_display = hb[11:16] if len(hb) > 16 else hb

            task_display = task[:40] if task and "未设置" not in task else "!! no task"
            self.tree.insert("", "end", values=(dot, display_name,
                              task_display, len(s.get("claimed_files", [])),
                              hb_display), tags=(color_tag,))

        self.tree.tag_configure("st_active", foreground=GREEN)
        self.tree.tag_configure("st_stale", foreground=YELLOW)
        self.tree.tag_configure("st_ended", foreground=FG_DIM)
        self.tree.tag_configure("st_no_task", foreground=RED)

        # 知识面板
        self.kn_text.config(state="normal")
        self.kn_text.delete("1.0", "end")
        lines = []
        if kn:
            lines.append(f"Knowledge: {len(kn)}")
            for fp, k in list(kn.items())[:8]:
                conf = k.get("confidence", 0)
                cat = k.get("category", "")
                text = k.get("text", "")[:50]
                star = "★" if conf >= 0.8 else ""
                lines.append(f"  [{conf:.1f}]{star} ({cat}) {text}")
        if decs:
            lines.append(f"\nDecisions: {decs}")
            for d in state.get("decisions", [])[-3:]:
                lines.append(f"  - {d.get('decision', '')[:50]}")
        if bps:
            lines.append(f"\nBug Patterns: {bps}")
            for b in state.get("bug_patterns", [])[-3:]:
                lines.append(f"  - {b.get('pattern', '')[:50]}")
        self.kn_text.insert("1.0", "\n".join(lines) if lines else "(no knowledge yet)")
        self.kn_text.config(state="disabled")

        # 健康检查
        self.health_text.config(state="normal")
        self.health_text.delete("1.0", "end")
        checks = []
        checks.append(("state.json", True))
        events_path = os.path.join(os.path.dirname(self.state_file), "events.log")
        checks.append(("events.log", os.path.isfile(events_path)))
        dead_locks = 0
        for proj, lock in state.get("build_locks", {}).items():
            owner = lock if isinstance(lock, str) else lock.get("session_id", "")
            if owner not in sessions or sessions[owner].get("status") != "active":
                dead_locks += 1
        checks.append(("build_locks", dead_locks == 0))
        checks.append(("sessions", len(active) > 0 or True))
        for name, ok in checks:
            icon = "OK" if ok else "ERR"
            self.health_text.insert("end", f"[{icon}] {name}\n")
        self.health_text.config(state="disabled")

    def _on_close(self):
        self._running = False
        self.root.destroy()


def main():
    project_dir = None
    if len(sys.argv) > 1:
        project_dir = sys.argv[1]

    state_file = find_state_file(project_dir)
    root = tk.Tk()
    app = BlackboardMonitor(root, state_file)
    root.mainloop()


if __name__ == "__main__":
    main()
