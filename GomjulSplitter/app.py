"""Korean Windows desktop interface. Run with python app.py or RUN_WINDOWS.cmd."""
from __future__ import annotations

import os
import queue
import subprocess
import threading
import tkinter as tk
from pathlib import Path
from tkinter import filedialog, messagebox, ttk

from splitter import MB, Cancelled, Runner, Settings, SplitError, probe, seconds_text, split_movie
from setup_tools import ensure_tools


class App(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title("곰줄 영화 분할기 · 0.2")
        self.geometry("1000x780")
        self.minsize(900, 700)
        self.configure(bg="#f3f5f8")
        self.option_add("*Font", ("맑은 고딕", 10))
        self.events = queue.Queue()
        self.cancel_event = threading.Event()
        self.busy = False
        self.output_folder = None
        self.audio_indices = []
        self.source = tk.StringVar()
        self.subtitle = tk.StringVar()
        self.output = tk.StringVar(value=str(Path.home() / "Videos" / "Gomjul_Output"))
        self.limit = tk.StringVar(value="250MB · 업로드 여유 모드 (권장)")
        self.height = tk.StringVar(value="원본 해상도")
        self.offset = tk.StringVar(value="0")
        self.threshold = tk.StringVar(value="0.30")
        self.window = tk.StringVar(value="10")
        self.encoding = tk.StringVar(value="auto")
        self.fallback = tk.BooleanVar(value=True)
        self.protect = tk.BooleanVar(value=True)
        self.full_verify = tk.BooleanVar(value=True)
        self.info = tk.StringVar(value="원본 영상과 선택 사항인 SRT를 고르세요. 원본 파일은 수정하지 않습니다.")
        self.status = tk.StringVar(value="준비")
        self.part_status = tk.StringVar(value="완료된 파일이 여기에 표시됩니다.")
        style = ttk.Style(self)
        style.theme_use("clam")
        style.configure("TFrame", background="#f3f5f8")
        style.configure("TLabel", background="#f3f5f8", foreground="#243048")
        style.configure("Title.TLabel", font=("맑은 고딕", 22, "bold"))
        style.configure("TButton", padding=(12, 7))
        style.configure("Accent.TButton", background="#2368d8", foreground="white", font=("맑은 고딕", 11, "bold"))
        style.map("Accent.TButton", background=[("active", "#1854b8"), ("disabled", "#91aad0")])
        style.configure("Treeview", rowheight=28, font=("맑은 고딕", 9))
        outer = ttk.Frame(self, padding=20)
        outer.pack(fill="both", expand=True)
        ttk.Label(outer, text="곰줄 영화 분할기", style="Title.TLabel").pack(anchor="w")
        ttk.Label(outer, text="장면 전환을 찾아 나누고, 파일 용량과 원본 시간을 확인합니다.").pack(anchor="w", pady=(2, 12))
        tabs = ttk.Notebook(outer)
        tabs.pack(fill="both", expand=True)
        main = ttk.Frame(tabs, padding=12)
        settings_tab = ttk.Frame(tabs, padding=15)
        help_tab = ttk.Frame(tabs, padding=15)
        tabs.add(main, text="  영화 분할  ")
        tabs.add(settings_tab, text="  고급 설정  ")
        tabs.add(help_tab, text="  사용법 · 제한  ")
        self.controls = []
        files = ttk.Frame(main)
        files.pack(fill="x")
        files.columnconfigure(1, weight=1)
        for row, (label, variable, command) in enumerate([
            ("원본 영화", self.source, self.pick_source),
            ("자막 (선택)", self.subtitle, self.pick_subtitle),
            ("저장 위치", self.output, self.pick_output),
        ]):
            ttk.Label(files, text=label, width=10).grid(row=row, column=0, sticky="w", pady=4)
            entry = ttk.Entry(files, textvariable=variable)
            entry.grid(row=row, column=1, sticky="ew", padx=(0, 8))
            button = ttk.Button(files, text="선택", command=command)
            button.grid(row=row, column=2)
            self.controls += [entry, button]
        clear = ttk.Button(files, text="자막 안 넣기", command=lambda: self.subtitle.set(""))
        clear.grid(row=1, column=3, padx=(8, 0))
        self.controls.append(clear)
        ttk.Label(main, text="자막을 비워 두면 영상만 분할합니다. SRT를 넣으면 Part별 SRT를 함께 만듭니다.").pack(anchor="w", pady=(8, 0))
        ttk.Label(main, textvariable=self.info, wraplength=900).pack(anchor="w", pady=(8, 10))
        size_row = ttk.Frame(main)
        size_row.pack(fill="x", pady=(0, 12))
        ttk.Label(size_row, text="파일 상한  ").pack(side="left")
        size_choice = ttk.Combobox(size_row, textvariable=self.limit, state="readonly", width=38,
                                  values=["250MB · 업로드 여유 모드 (권장)", "512MB · 적은 파일 모드"])
        size_choice.pack(side="left")
        self.controls.append(size_choice)
        ttk.Label(settings_tab, text="기본값으로 자동 분할할 수 있습니다. 필요한 항목만 변경하세요.").pack(anchor="w", pady=(0, 15))
        options = ttk.Frame(settings_tab)
        options.pack(fill="x")
        for col in [1, 3]:
            options.columnconfigure(col, weight=1)
        def choice(row, col, label, variable, values, width=28):
            ttk.Label(options, text=label).grid(row=row, column=col, sticky="w", padx=(0, 8), pady=5)
            combo = ttk.Combobox(options, textvariable=variable, values=values, state="readonly", width=width)
            combo.grid(row=row, column=col+1, sticky="ew", padx=(0, 15))
            self.controls.append(combo)
            return combo
        choice(0, 2, "출력 화면", self.height, ["원본 해상도", "최대 1080p", "최대 720p"], 18)
        ttk.Label(options, text="오디오").grid(row=1, column=0, sticky="w")
        self.audio = ttk.Combobox(options, values=["첫 번째 오디오"], state="readonly", width=28)
        self.audio.current(0)
        self.audio.grid(row=1, column=1, sticky="ew", padx=(0, 15))
        self.controls.append(self.audio)
        self.source.trace_add("write", self.source_changed)
        choice(1, 2, "자막 인코딩", self.encoding, ["auto", "utf-8-sig", "cp949", "utf-16"], 18)
        advanced = ttk.Frame(settings_tab)
        advanced.pack(fill="x", pady=(5, 4))
        for col, (label, var) in enumerate([("전환 검색 ±초", self.window), ("감지 기준", self.threshold), ("자막 보정 초", self.offset)]):
            ttk.Label(advanced, text=label).grid(row=0, column=col*2, padx=(0 if col == 0 else 15, 8))
            entry = ttk.Entry(advanced, textvariable=var, width=8)
            entry.grid(row=0, column=col*2+1)
            self.controls.append(entry)
        checks = ttk.Frame(settings_tab)
        checks.pack(fill="x", pady=4)
        for label, var in [("자막 중간 경계 피하기", self.protect), ("전환 없으면 시간 기준으로 대체", self.fallback), ("완료 후 전체 재생 검사", self.full_verify)]:
            item = ttk.Checkbutton(checks, text=label, variable=var)
            item.pack(side="left", padx=(0, 13))
            self.controls.append(item)
        ttk.Label(main, text="약 8% 여유를 두고 계산합니다. 영상은 겹치지 않으며, MP4 재인코딩으로 화질 변화가 있을 수 있습니다.",
                  wraplength=890).pack(anchor="w", pady=(2, 8))
        actions = ttk.Frame(main)
        actions.pack(fill="x", pady=(0, 8))
        self.start_button = ttk.Button(actions, text="자동 분할 시작 / 이어하기", style="Accent.TButton", command=self.start)
        self.start_button.pack(side="left")
        self.stop_button = ttk.Button(actions, text="중지", command=self.cancel, state="disabled")
        self.stop_button.pack(side="left", padx=8)
        self.open_button = ttk.Button(actions, text="결과 폴더 열기", command=self.open_folder, state="disabled")
        self.open_button.pack(side="left")
        ttk.Label(actions, textvariable=self.status).pack(side="right")
        ttk.Label(main, textvariable=self.part_status, wraplength=890).pack(anchor="w", pady=(0, 3))
        self.progress = ttk.Progressbar(main, mode="determinate", maximum=100)
        self.progress.pack(fill="x", pady=(0, 8))
        table_frame = ttk.Frame(main)
        table_frame.pack(fill="both", expand=True)
        columns = ("part", "start", "end", "size", "boundary")
        self.table = ttk.Treeview(table_frame, columns=columns, show="headings", height=5)
        for name, title, width in [("part", "파일", 140), ("start", "원본 시작", 140), ("end", "원본 종료", 140), ("size", "용량", 100), ("boundary", "분할 기준", 190)]:
            self.table.heading(name, text=title)
            self.table.column(name, width=width, minwidth=75)
        scroll = ttk.Scrollbar(table_frame, command=self.table.yview)
        self.table.configure(yscrollcommand=scroll.set)
        self.table.pack(side="left", fill="both", expand=True)
        scroll.pack(side="right", fill="y")
        self.log = tk.Text(main, height=4, bg="#e9eef5", fg="#34425c", relief="flat", wrap="word", state="disabled")
        self.log.pack(fill="x", pady=(8, 0))
        help_text = (
            "1. 영화를 선택하세요. SRT는 필요한 경우에만 선택하세요.\n"
            "2. 기본 250MB 또는 512MB를 선택하고 자동 분할 시작을 누르세요.\n"
            "3. 영상 도구가 없으면 자동으로 준비하고, 분할·용량·재생 검사를 진행합니다.\n"
            "4. 중단 후 같은 영화·자막·설정·저장 위치로 시작하면 완료 파일을 확인해 이어갑니다.\n\n"
            "±10초의 의미\n"
            "용량으로 계산한 예상 분할점 앞뒤 10초에서 화면 변화를 찾습니다.\n"
            "이는 앞뒤 영상을 겹치는 설정이 아닙니다. 컷 감지는 이야기의 장면/사건 이해가 아닙니다.\n"
            "전환을 찾지 못하면 체크한 옵션에 따라 시간 기준으로 대체하고 기록하거나 작업을 중단합니다.\n\n"
            "용량과 화질\n"
            "250MB=250,000,000바이트, 512MB=512,000,000바이트입니다.\n"
            "약 230MB / 471MB를 목표로 계산하고, 출력 후 실제 상한을 검사합니다.\n"
            "H.264 재인코딩과 선택한 오디오의 AAC 스테레오 변환을 사용합니다. 무손실 분할은 아닙니다.\n"
            "2회 인코딩과 전체 재생 검사는 시간이 걸립니다. 원본 프레임률을 60fps로 올리지 않습니다.\n"
            "HDR 색상 변환과 무손실 모드는 지원하지 않습니다.\n"
            "이어하기는 완료된 Part부터입니다. 처리 도중 멈춘 Part는 처음부터 다시 만듭니다.\n\n"
            "자막과 원본 시간\n"
            "원본 자막을 그대로 보존하고, Part별 자막은 0초 기준으로 만듭니다.\n"
            "SRT를 선택하지 않으면 자막을 검색·추출·생성하지 않습니다.\n"
            "SRT는 별도 파일입니다. 화면에 태우지 않으며, 재생 시 같은 이름의 SRT를 여세요.\n"
            "원본 화면에 이미 박힌 자막은 영상의 일부이므로 그대로 남습니다.\n"
            "자막 보정: 영상 시간 = SRT 시간 + 보정 초. 자막이 2초 늦다면 -2를 입력하세요.\n"
            "시간표는 입력 영화 파일의 시작을 0초로 기록합니다. 다른 판본의 시간으로 자동 변환하지 않습니다.\n"
            "화면 밖 대사도 있을 수 있으므로 ‘자막 중간 피하기’가 대사 보호를 보장하지 않습니다.\n\n"
            "함께 올릴 파일\n"
            "Part_001.mp4 … + 원본_시간표.csv (자막을 선택했다면 original_subtitles.srt도)\n"
            "업로드_안내.txt에는 한 영화로 분석할 때 필요한 시간 기준이 들어 있습니다.\n"
            "크기 검사와 재생 검사는 업로드 서비스의 성공을 보장하지 않습니다.\n"
            "영화는 외부로 전송하지 않습니다. 최초 영상 도구 다운로드만 인터넷을 사용합니다."
        )
        text = tk.Text(help_tab, wrap="word", relief="flat", bg="#f3f5f8", fg="#243048", padx=8, pady=8)
        text.insert("1.0", help_text)
        text.configure(state="disabled")
        text.pack(fill="both", expand=True)
        self.protocol("WM_DELETE_WINDOW", self.close)
        self.after(100, self.poll)

    def pick_source(self):
        selected = filedialog.askopenfilename(title="원본 영화", filetypes=[("영상 파일", "*.mp4 *.mkv *.mov *.avi *.m4v *.ts *.webm"), ("모든 파일", "*.*")])
        if not selected:
            return
        self.source.set(selected)
        self.subtitle.set("")
        self.info.set("영상 정보를 확인하는 중입니다…")
        self.audio_indices = []
        self.audio.configure(values=["첫 번째 오디오"])
        self.audio.current(0)
        def work():
            try:
                info = probe(Path(selected), Runner())
                self.events.put({"type": "metadata", "source": selected, "info": info})
            except Exception as exc:
                self.events.put({"type": "metadata_error", "source": selected, "message": str(exc)})
        threading.Thread(target=work, daemon=True).start()

    def source_changed(self, *_):
        self.subtitle.set("")
        self.audio_indices = []
        self.audio.configure(values=["첫 번째 오디오"])
        self.audio.current(0)

    def pick_subtitle(self):
        value = filedialog.askopenfilename(title="SRT 자막 (선택)", filetypes=[("SubRip", "*.srt")])
        if value:
            self.subtitle.set(value)

    def pick_output(self):
        value = filedialog.askdirectory(title="저장할 상위 폴더")
        if value:
            self.output.set(value)

    def set_busy(self, busy):
        self.busy = busy
        for control in self.controls:
            control.configure(state="disabled" if busy else ("readonly" if isinstance(control, ttk.Combobox) else "normal"))
        self.start_button.configure(state="disabled" if busy else "normal")
        self.stop_button.configure(state="normal" if busy else "disabled")

    def start(self):
        if self.busy:
            return
        try:
            source = Path(self.source.get().strip())
            if not source.is_file():
                raise ValueError("원본 영화를 선택하세요.")
            if not self.output.get().strip():
                raise ValueError("저장 위치를 선택하세요.")
            output = Path(self.output.get().strip())
            subtitle = Path(self.subtitle.get().strip()) if self.subtitle.get().strip() else None
            if subtitle and not subtitle.is_file():
                raise ValueError("선택한 자막 파일이 없습니다. 다시 선택하거나 ‘자막 안 넣기’를 누르세요.")
            selected = self.audio.current()
            audio_index = self.audio_indices[selected] if self.audio_indices and 0 < selected < len(self.audio_indices) else None
            settings = Settings(max_bytes=(250 if self.limit.get().startswith("250") else 512) * MB,
                                window=float(self.window.get()), scene_threshold=float(self.threshold.get()),
                                subtitle_offset=float(self.offset.get()), subtitle_encoding=self.encoding.get(),
                                max_height={"원본 해상도": 0, "최대 1080p": 1080, "최대 720p": 720}[self.height.get()],
                                audio_index=audio_index, allow_time_fallback=self.fallback.get(),
                                avoid_subtitle=self.protect.get(), full_verify=self.full_verify.get())
            settings.validate()
        except (ValueError, SplitError) as exc:
            messagebox.showerror("설정 확인", str(exc))
            return
        self.table.delete(*self.table.get_children())
        self.cancel_event.clear()
        self.set_busy(True)
        self.output_folder = None
        self.open_button.configure(state="disabled")
        self.progress["value"] = 0
        self.add_log("작업을 시작합니다. 동일한 작업이 있으면 검증 후 이어갑니다.")
        def work():
            try:
                runner = Runner(self.cancel_event, self.events.put)
                ensure_tools(runner)
                split_movie(source, output, settings, subtitle, runner)
            except Cancelled as exc:
                self.events.put({"type": "cancelled", "message": str(exc)})
            except Exception as exc:
                self.events.put({"type": "error", "message": str(exc)})
            finally:
                self.events.put({"type": "idle"})
        threading.Thread(target=work, daemon=True).start()

    def add_log(self, message):
        self.log.configure(state="normal")
        self.log.insert("end", message + "\n")
        self.log.see("end")
        self.log.configure(state="disabled")

    def poll(self):
        for _ in range(100):
            try:
                event = self.events.get_nowait()
            except queue.Empty:
                break
            kind = event["type"]
            if kind == "metadata" and not self.busy and event["source"] == self.source.get():
                info = event["info"]
                v = info["video"]
                self.info.set(f"{Path(event['source']).stat().st_size / MB:,.1f}MB · {seconds_text(info['duration_seconds'])} · {v.get('width')}×{v.get('height')} · 원본 시작 기준으로 기록")
                tracks = [s for s in info["streams"] if s.get("codec_type") == "audio"]
                self.audio_indices = [s["index"] for s in tracks]
                labels = [f"{i+1}. {s.get('tags', {}).get('language', '언어 미표기')} · {s.get('codec_name', '')} · {s.get('channels', '?')}채널" for i, s in enumerate(tracks)]
                self.audio.configure(values=labels or ["오디오 없음"])
                self.audio.current(0)
            elif kind == "metadata_error" and not self.busy and event["source"] == self.source.get():
                self.info.set("영화를 선택했습니다. 시작하면 필요한 영상 도구를 자동으로 준비합니다.")
            elif kind == "stage":
                self.status.set(event["message"])
                self.progress["value"] = 0
            elif kind == "progress":
                self.progress["value"] = event["fraction"] * 100
            elif kind == "folder":
                self.output_folder = event["path"]
                self.open_button.configure(state="normal")
            elif kind == "part_start":
                self.part_status.set(event["message"])
                self.add_log(event["message"])
            elif kind == "part_done":
                p = event["part"]
                label = {"scene": "장면 전환", "time_fallback": "시간 대체", "subtitle_end_fallback": "자막 끝 대체", "end_of_file": "영화 끝"}[p["boundary_kind"]]
                self.table.insert("", "end", values=(p["file"], seconds_text(p["source_start_seconds"]), seconds_text(p["source_end_seconds"]), f"{p['size_bytes']/MB:.2f}MB", label))
            elif kind == "log":
                self.add_log(event["message"])
            elif kind == "done":
                self.progress["value"] = 100
                self.status.set("전체 완료")
                self.part_status.set(f"{event['count']}개 파일과 원본 시간표를 저장했습니다.")
                self.add_log("완료. 원본_시간표.csv도 함께 올려주세요." + (" 선택한 자막도 함께 저장했습니다." if event.get("has_subtitle") else " 자막은 생성하지 않았습니다."))
                messagebox.showinfo("분할 완료", f"{event['count']}개 파일을 저장했습니다.\n\n{event['path']}\n\n시간 기준으로 대체한 경계가 있는지 시간표를 확인하세요.")
            elif kind in ("error", "cancelled"):
                self.status.set("중지됨" if kind == "cancelled" else "확인 필요")
                self.add_log(event["message"])
                if kind == "error":
                    messagebox.showerror("작업을 완료하지 못했습니다", event["message"][-2500:])
            elif kind == "idle":
                self.set_busy(False)
        self.after(100, self.poll)

    def cancel(self):
        self.cancel_event.set()
        self.status.set("현재 처리를 중지하는 중…")
        self.stop_button.configure(state="disabled")

    def open_folder(self):
        if self.output_folder:
            if os.name == "nt":
                os.startfile(self.output_folder)
            else:
                subprocess.Popen(["xdg-open", self.output_folder])

    def close(self):
        if self.busy:
            self.cancel()
            self.add_log("중지 처리가 끝나면 창을 닫아주세요. 검증 완료한 파일은 남아 있습니다.")
        else:
            self.destroy()


if __name__ == "__main__":
    import sys
    if len(sys.argv) == 3 and sys.argv[1] == "--self-test":
        from smoke_test import run
        raise SystemExit(run(Path(sys.argv[2])))
    App().mainloop()
